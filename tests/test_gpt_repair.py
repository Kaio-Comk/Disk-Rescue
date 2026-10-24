"""
tests/test_gpt_repair.py — Testes unitários para o parser e motor de reparo GPT.
"""
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
