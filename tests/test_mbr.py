"""
tests/test_mbr.py — Testes unitários para parsing e reparo de tabelas MBR.
"""
from pathlib import Path

from core.partition.mbr import (
    MBRPartitionEntry,
    parse_mbr,
    rebuild_mbr_bytes,
    repair_mbr_partition,
)


def test_parse_and_rebuild_mbr():
    partitions = [
        MBRPartitionEntry(
            index=1,
            is_active=True,
            type_id=0x07,  # NTFS
            type_name="NTFS / exFAT",
            start_lba=2048,
            sector_count=204800
        ),
        MBRPartitionEntry(
            index=2,
            is_active=False,
            type_id=0x0B,  # FAT32
            type_name="FAT32",
            start_lba=206848,
            sector_count=100000
        )
    ]

    raw_mbr = rebuild_mbr_bytes(partitions, bootstrap_code=b"BOOTSTRAP_CODE" * 10)
    assert len(raw_mbr) == 512
    assert raw_mbr[510:512] == b"\x55\xaa"

    table = parse_mbr(raw_mbr)
    assert table is not None
    assert table.has_valid_signature is True
    assert table.is_protective_gpt is False

    p1 = table.partitions[0]
    assert p1.index == 1
    assert p1.is_active is True
    assert p1.type_id == 0x07
    assert p1.start_lba == 2048
    assert p1.sector_count == 204800

    p2 = table.partitions[1]
    assert p2.index == 2
    assert p2.type_id == 0x0B
    assert p2.start_lba == 206848


def test_repair_mbr_partition_in_file(tmp_path: Path):
    mbr_file = tmp_path / "mock_mbr.bin"
    # Cria MBR com partição 1 apontando para LBA errado (ex: 63 em vez de 2048)
    initial_parts = [
        MBRPartitionEntry(1, True, 0x07, "NTFS", 63, 10000)
    ]
    mbr_file.write_bytes(rebuild_mbr_bytes(initial_parts))

    # Repara partição 1 para começar no LBA 2048 com 50000 setores
    backup_dir = tmp_path / "backups"
    ok = repair_mbr_partition(
        str(mbr_file),
        target_index=1,
        new_start_lba=2048,
        new_sector_count=50000,
        backup_dir=backup_dir
    )
    assert ok is True

    # Verifica se foi salvo o backup do MBR antigo
    assert list(backup_dir.glob("mbr_backup_*.bin"))

    # Verifica se o MBR foi atualizado corretamente
    table_after = parse_mbr(mbr_file.read_bytes())
    assert table_after.partitions[0].start_lba == 2048
    assert table_after.partitions[0].sector_count == 50000


def test_repair_mbr_rejects_invalid_index_and_overlapping(tmp_path: Path):
    """P1 Teste: Garante que repair_mbr_partition recuse índices inexistentes e sobreposições de partição."""
    mbr_file = tmp_path / "mock_mbr_overlap.bin"
    initial_parts = [
        MBRPartitionEntry(1, True, 0x07, "NTFS", 2048, 10000),   # LBA 2048..12047
        MBRPartitionEntry(2, False, 0x0B, "FAT32", 20000, 10000) # LBA 20000..29999
    ]
    mbr_file.write_bytes(rebuild_mbr_bytes(initial_parts))
    backup_dir = tmp_path / "backups_validation"

    # 1. Índice fora do intervalo (1..4)
    assert repair_mbr_partition(str(mbr_file), target_index=5, new_start_lba=35000, new_sector_count=5000, backup_dir=backup_dir) is False
    assert repair_mbr_partition(str(mbr_file), target_index=0, new_start_lba=35000, new_sector_count=5000, backup_dir=backup_dir) is False

    # 2. Conflito de sobreposição: Tenta redimensionar partição 1 para LBA 2048..25000 (colide com partição 2 que começa em 20000)
    assert repair_mbr_partition(str(mbr_file), target_index=1, new_start_lba=2048, new_sector_count=20000, backup_dir=backup_dir) is False

    # 3. Limites inválidos (start_lba < 1 ou count <= 0)
    assert repair_mbr_partition(str(mbr_file), target_index=1, new_start_lba=0, new_sector_count=1000, backup_dir=backup_dir) is False
    assert repair_mbr_partition(str(mbr_file), target_index=1, new_start_lba=1000, new_sector_count=0, backup_dir=backup_dir) is False


def test_repair_mbr_validates_backup_dir_before_mkdir(tmp_path: Path, monkeypatch):
    """P0 Teste: Garante que diretório de backup inseguro seja rejeitado antes de mkdir."""
    mbr_file = tmp_path / "mock_mbr.bin"
    mbr_file.write_bytes(b"\x00" * 512)
    unsafe_dir = tmp_path / "uncreated_unsafe_dir"

    def mock_assert_safe(source_str, target_path, is_directory=False):
        raise ValueError("Diretório de backup no mesmo disco.")

    monkeypatch.setattr("core.hardware.device_resolver.assert_safe_source_and_destination", mock_assert_safe)

    ok = repair_mbr_partition(str(mbr_file), target_index=1, new_start_lba=2048, new_sector_count=1000, backup_dir=unsafe_dir)
    assert ok is False
    assert not unsafe_dir.exists()

