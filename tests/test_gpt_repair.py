"""
tests/test_gpt_repair.py — Testes unitários para o parser e motor de reparo GPT.
"""
import io
import struct
import zlib
from pathlib import Path

import pytest

from core.filesystem.ntfs import parse_ntfs_boot_sector
from core.partition.gpt import (
    DEFAULT_SECTOR_SIZE,
    GPTPartitionEntry,
    GPTTable,
    parse_gpt,
    rebuild_gpt,
)


def _criar_gpt_sintetico() -> bytearray:
    """Cria uma imagem binária em memória com LBA 0 (MBR protector), LBA 1 (GPT Header) e LBA 2..33 (Partições)."""
    disk = bytearray(64 * 512) # 64 setores (32 KB)
    
    # LBA 1: Header GPT
    header = bytearray(512)
    header[0:8] = b"EFI PART"
    rev = 0x00010000
    h_size = 92
    cur_lba = 1
    backup_lba = 100000
    first_usable = 34
    last_usable = 99966
    disk_guid = b"\x01" * 16
    part_lba = 2
    num_entries = 128
    entry_size = 128
    
    # Cria duas partições: MSR e Basic Data
    p1 = GPTPartitionEntry(
        index=1,
        type_guid=b"\x16\xe3\xc9\xe3" + b"\x00" * 12,
        unique_guid=b"\x02" * 16,
        first_lba=2048,
        last_lba=34815,
        flags=0x8000000000000000,
        name="Microsoft reserved partition"
    )
    p2 = GPTPartitionEntry(
        index=2,
        type_guid=b"\xa2\xa0\xd0\xeb" + b"\x00" * 12,
        unique_guid=b"\x03" * 16,
        first_lba=34816, # Erro sintético (deslocado)
        last_lba=99000,
        flags=0x8000000000000000,
        name="Basic data partition"
    )
    
    entries = bytearray(num_entries * entry_size)
    entries[0:128] = p1.to_bytes(entry_size)
    entries[128:256] = p2.to_bytes(entry_size)
    
    part_crc = zlib.crc32(entries) & 0xFFFFFFFF
    
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        header, 0,
        b"EFI PART", rev, h_size, 0, 0,
        cur_lba, backup_lba, first_usable, last_usable,
        disk_guid, part_lba, num_entries, entry_size, part_crc
    )
    
    h_crc = zlib.crc32(header[:h_size]) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, h_crc)
    
    disk[512:1024] = header
    disk[1024 : 1024 + len(entries)] = entries
    return disk


def test_parse_gpt_valida_crc(tmp_path):
    img = tmp_path / "mock_disk.bin"
    img.write_bytes(_criar_gpt_sintetico())
    
    with open(img, "rb") as f:
        gpt = parse_gpt(f)
        
    assert gpt is not None
    assert gpt.is_header_crc_valid is True
    assert gpt.is_entries_crc_valid is True
    assert len(gpt.partitions) == 2
    assert gpt.partitions[0].first_lba == 2048
    assert gpt.partitions[1].first_lba == 34816


def test_rebuild_gpt_corrige_offset_e_recalcula_crc(tmp_path):
    img = tmp_path / "mock_disk.bin"
    img.write_bytes(_criar_gpt_sintetico())
    
    with open(img, "r+b") as f:
        gpt = parse_gpt(f)
        assert gpt is not None
        
        # Realinha partição 2 para LBA 567296
        gpt.partitions[1].first_lba = 567296
        gpt.partitions[1].last_lba = 945291255
        gpt.partitions[1].flags = 0x0
        
        new_header, new_entries = rebuild_gpt(gpt, gpt.partitions)
        f.seek(512)
        f.write(new_header)
        f.seek(1024)
        f.write(new_entries)
        f.flush()
        
    # Reabre e valida
    with open(img, "rb") as f:
        gpt_corrigido = parse_gpt(f)
        
    assert gpt_corrigido is not None
    assert gpt_corrigido.is_header_crc_valid is True
    assert gpt_corrigido.is_entries_crc_valid is True
    assert gpt_corrigido.partitions[1].first_lba == 567296
    assert gpt_corrigido.partitions[1].last_lba == 945291255
    assert gpt_corrigido.partitions[1].no_drive_letter is False


def test_gpt_parser_rejects_malformed_structures(tmp_path):
    """P1 Teste: Garante que o parser GPT rejeite tamanhos incoerentes e evite alocação abusiva de RAM."""
    raw = _criar_gpt_sintetico()
    
    # 1. header_size menor que 92 bytes
    corrupt_hsize = bytearray(raw)
    struct.pack_into("<I", corrupt_hsize, 512 + 12, 50)  # h_size = 50
    f1 = io.BytesIO(corrupt_hsize)
    assert parse_gpt(f1) is None

    # 2. num_entries astronômico (ex: 50.000 entradas)
    corrupt_entries = bytearray(raw)
    struct.pack_into("<I", corrupt_entries, 512 + 80, 50000)
    f2 = io.BytesIO(corrupt_entries)
    assert parse_gpt(f2) is None

    # 3. entry_size inválido (ex: 10 bytes)
    corrupt_esize = bytearray(raw)
    struct.pack_into("<I", corrupt_esize, 512 + 84, 10)
    f3 = io.BytesIO(corrupt_esize)
    assert parse_gpt(f3) is None


def test_repair_gpt_partition_offset_syncs_backup_and_rejects_overlaps(tmp_path):
    """
    P1 Teste: Valida que repair_gpt_partition_offset sincroniza a cópia de backup no fim do disco
    e rejeita sobreposições e limites inválidos.
    """
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    # Cria disco sintético com espaço suficiente para as partições
    total_sectors = 100_000
    disk_bytes = bytearray(64 * 512)
    base_gpt = _criar_gpt_sintetico()
    disk_bytes[:len(base_gpt)] = base_gpt
    disk_bytes.extend(b"\x00" * ((total_sectors - 64) * 512))

    # Atualiza cabeçalho primário: backup no LBA 99999 e last_usable no LBA 90000
    struct.pack_into("<Q", disk_bytes, 512 + 32, 99999)  # backup_lba = 99999
    struct.pack_into("<Q", disk_bytes, 512 + 48, 90000)  # last_usable = 90000
    # Recalcula CRC do cabeçalho primário
    disk_bytes[512 + 16 : 512 + 20] = b"\x00\x00\x00\x00"
    h_crc = zlib.crc32(disk_bytes[512 : 512 + 92]) & 0xFFFFFFFF
    struct.pack_into("<I", disk_bytes, 512 + 16, h_crc)

    img = tmp_path / "mock_disk_gpt_full.bin"
    img.write_bytes(disk_bytes)
    backup_dir = tmp_path / "backups_gpt"

    # 1. Tenta reparar para fora da área utilizável (LBA 91000..92000 > last_usable 90000)
    fs_out_of_bounds = DiscoveredFilesystem("NTFS", start_lba=91000, total_sectors=1000, size_bytes=1000 * 512)
    res_err = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs_out_of_bounds, backup_dir=backup_dir)
    assert res_err.success is False
    assert "ultrapassa área utilizável" in res_err.message

    # 2. Tenta reparar para sobrepor partição 1 (partição 1 está em 2048..34815; tenta 2000..3000)
    fs_overlap = DiscoveredFilesystem("NTFS", start_lba=2000, total_sectors=1001, size_bytes=1001 * 512)
    res_overlap = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs_overlap, backup_dir=backup_dir)
    assert res_overlap.success is False
    assert "sobrepõe a partição 1" in res_overlap.message

    # 3. Reparo válido em LBA 40000..50000 (dentro dos limites e sem sobreposição)
    fs_valid = DiscoveredFilesystem("NTFS", start_lba=40000, total_sectors=10001, size_bytes=10001 * 512)
    res_ok = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs_valid, backup_dir=backup_dir)
    assert res_ok.success is True

    # Verifica se os bytes da GPT primária foram atualizados
    with open(img, "rb") as f:
        reopened = parse_gpt(f)
    assert reopened is not None
    assert reopened.partitions[1].first_lba == 40000
    assert reopened.partitions[1].last_lba == 50000
    # Verifica que flags originais foram preservadas
    assert reopened.partitions[1].flags == 0x0


def test_repair_gpt_validates_backup_dir_before_mkdir(tmp_path, monkeypatch):
    """P0 Teste: Garante que diretório de backup inseguro seja rejeitado antes de mkdir."""
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    img = tmp_path / "mock_disk.bin"
    img.write_bytes(_criar_gpt_sintetico())
    unsafe_dir = tmp_path / "uncreated_unsafe_gpt_dir"

    def mock_assert_safe(source_str, target_path, is_directory=False):
        raise ValueError("Diretório de backup inseguro no mesmo disco.")

    monkeypatch.setattr("core.hardware.device_resolver.assert_safe_source_and_destination", mock_assert_safe)

    fs = DiscoveredFilesystem("NTFS", start_lba=40000, total_sectors=1000, size_bytes=1000 * 512)
    res = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs, backup_dir=unsafe_dir)
    assert res.success is False
    assert not unsafe_dir.exists()


def test_repair_gpt_creates_both_primary_and_backup_snapshots(tmp_path):
    """P0 Teste: Garante que snapshots preventivos tanto da GPT Primária quanto da de Backup sejam gravados."""
    from core.filesystem.scanner import DiscoveredFilesystem
    from core.recovery.repair import repair_gpt_partition_offset

    total_sectors = 100_000
    disk_bytes = bytearray(64 * 512)
    base_gpt = _criar_gpt_sintetico()
    disk_bytes[:len(base_gpt)] = base_gpt
    disk_bytes.extend(b"\x00" * ((total_sectors - 64) * 512))

    struct.pack_into("<Q", disk_bytes, 512 + 32, 99999)  # backup_lba = 99999
    struct.pack_into("<Q", disk_bytes, 512 + 48, 90000)  # last_usable = 90000
    disk_bytes[512 + 16 : 512 + 20] = b"\x00\x00\x00\x00"
    h_crc = zlib.crc32(disk_bytes[512 : 512 + 92]) & 0xFFFFFFFF
    struct.pack_into("<I", disk_bytes, 512 + 16, h_crc)

    img = tmp_path / "dual_snap_disk.bin"
    img.write_bytes(disk_bytes)
    backup_dir = tmp_path / "dual_snapshots"

    fs_valid = DiscoveredFilesystem("NTFS", start_lba=40000, total_sectors=10001, size_bytes=10001 * 512)
    res = repair_gpt_partition_offset(str(img), target_partition_index=2, discovered_fs=fs_valid, backup_dir=backup_dir)
    assert res.success is True

    # Valida existência física e integridade de ambos os snapshots
    primary_snaps = list(backup_dir.glob("backup_gpt_primary_*.bin"))
    backup_snaps = list(backup_dir.glob("backup_gpt_secondary_*.bin"))

    assert len(primary_snaps) >= 1
    assert len(backup_snaps) >= 1
    assert primary_snaps[0].stat().st_size == 34 * 512
    assert backup_snaps[0].stat().st_size == 33 * 512


