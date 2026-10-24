"""
tests/test_fat_exfat.py — Testes unitários para detecção e parsing de FAT32 e exFAT.
"""
import struct
from pathlib import Path

from core.filesystem.fat_exfat import parse_exfat_boot_sector, parse_fat32_boot_sector
from core.filesystem.scanner import scan_boot_sectors


def _build_synthetic_fat32_vbr(start_lba: int, total_sectors: int) -> bytes:
    data = bytearray(512)
    data[0:3] = b"\xeb\x58\x90"
    data[3:11] = b"MSDOS5.0"
    struct.pack_into("<H", data, 11, 512)   # 512 bytes/sector
    data[13] = 8                            # 8 sectors/cluster
    struct.pack_into("<H", data, 14, 32)    # 32 reserved sectors
    data[16] = 2                            # 2 FATs
    struct.pack_into("<I", data, 32, total_sectors)
    struct.pack_into("<I", data, 36, 1024)  # sectors per FAT
    struct.pack_into("<I", data, 44, 2)     # root cluster = 2
    data[71:82] = b"TEST_FAT32 "
    data[82:90] = b"FAT32   "
    data[510:512] = b"\x55\xaa"
    return bytes(data)


def _build_synthetic_exfat_vbr(start_lba: int, total_sectors: int) -> bytes:
    data = bytearray(512)
    data[0:3] = b"\xeb\x76\x90"
    data[3:11] = b"EXFAT   "
    struct.pack_into("<Q", data, 64, start_lba)
    struct.pack_into("<Q", data, 72, total_sectors)
    struct.pack_into("<I", data, 80, 24)    # fat offset
    struct.pack_into("<I", data, 88, 256)   # cluster heap offset
    struct.pack_into("<I", data, 96, 4)     # root cluster = 4
    data[108] = 9                           # 2^9 = 512 bytes per sector
    data[109] = 3                           # 2^3 = 8 sectors per cluster
    data[510:512] = b"\x55\xaa"
    return bytes(data)


def test_parse_fat32_and_exfat():
    f32_raw = _build_synthetic_fat32_vbr(2048, 50000)
    f32 = parse_fat32_boot_sector(f32_raw, 2048)
    assert f32 is not None
    assert f32.is_valid is True
    assert f32.total_sectors == 50000
    assert f32.root_cluster == 2
    assert f32.bytes_per_sector == 512

    exf_raw = _build_synthetic_exfat_vbr(100000, 200000)
    exf = parse_exfat_boot_sector(exf_raw, 100000)
    assert exf is not None
    assert exf.is_valid is True
    assert exf.total_sectors == 200000
    assert exf.bytes_per_sector == 512
    assert exf.root_cluster == 4


def test_scan_boot_sectors_discovers_fat32_and_exfat(tmp_path: Path):
    disk_file = tmp_path / "multi_fs_disk.img"
    # Aloca 3000 setores
    with open(disk_file, "wb") as f:
        f.seek(3000 * 512 - 1)
        f.write(b"\x00")

    # Grava FAT32 no LBA 100
    f32_bytes = _build_synthetic_fat32_vbr(100, 1000)
    with open(disk_file, "r+b") as f:
        f.seek(100 * 512)
        f.write(f32_bytes)

        # Grava exFAT no LBA 1500
        exf_bytes = _build_synthetic_exfat_vbr(1500, 1500)
        f.seek(1500 * 512)
        f.write(exf_bytes)

    discovered = scan_boot_sectors(str(disk_file), max_lba=3000)
    types = [d.fs_type for d in discovered]
    assert "FAT32" in types
    assert "exFAT" in types

    f32_disc = next(d for d in discovered if d.fs_type == "FAT32")
    assert f32_disc.start_lba == 100
    assert f32_disc.total_sectors == 1000

    exf_disc = next(d for d in discovered if d.fs_type == "exFAT")
    assert exf_disc.start_lba == 1500
    assert exf_disc.total_sectors == 1500
